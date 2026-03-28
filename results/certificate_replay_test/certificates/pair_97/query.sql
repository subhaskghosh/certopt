SELECT
  firstname,
  lastname,
  COALESCE(city, NULL) AS CITY,
  state
FROM PERSON AS P
LEFT JOIN ADDRESS AS A
  ON a.personid = p.personid