SELECT
  p.firstname AS FIRSTNAME,
  p.lastname AS LASTNAME,
  a.city AS CITY,
  a.state AS STATE
FROM PERSON AS P
LEFT JOIN ADDRESS AS A
  ON a.personid = p.personid