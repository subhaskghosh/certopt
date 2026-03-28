SELECT
  p.firstname,
  p.lastname,
  a.city,
  COALESCE(a.state, NULL) AS STATE
FROM PERSON AS P
LEFT JOIN ADDRESS AS A
  ON a.personid = p.personid