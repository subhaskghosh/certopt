SELECT
  p.firstname,
  p.lastname,
  q.city,
  q.state
FROM PERSON AS P
LEFT JOIN ADDRESS AS Q
  ON p.personid = q.personid